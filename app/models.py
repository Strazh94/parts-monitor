"""Database models.

Stock history is stored in the append-only snapshots table (spec §6) —
values of previous days are never overwritten.
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class StockStatus(str, enum.Enum):
    """Canonical availability statuses (spec §16)."""

    IN_STOCK = "in_stock"          # in stock (with or without a quantity)
    OUT_OF_STOCK = "out_of_stock"  # out of stock -> 0
    ON_ORDER = "on_order"          # on order -> stock unknown
    MANY = "many"                  # "many" -> quantity unknown
    UNKNOWN = "unknown"            # data not retrieved


class EngineType(str, enum.Enum):
    """Site parsing engine type."""

    HTTP = "http"          # requests/httpx + BeautifulSoup
    PLAYWRIGHT = "playwright"  # for JavaScript sites


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


class RunTrigger(str, enum.Enum):
    AUTO = "auto"    # by schedule
    MANUAL = "manual"  # the "Run data collection" button


class ChangeType(str, enum.Enum):
    """Type of change between days (spec §7, 15)."""

    SALES = "sales"                    # stock decrease -> estimated sale
    REPLENISHMENT = "replenishment"    # stock increase -> restock
    PRICE_DOWN = "price_down"
    PRICE_UP = "price_up"
    APPEARED = "appeared"              # new product
    DISAPPEARED = "disappeared"        # product disappeared from the catalog
    STATUS_ONLY = "status_only"        # availability status changed without quantity


class Competitor(Base):
    """Competitor website (spec §4, 23)."""

    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    engine: Mapped[EngineType] = mapped_column(
        Enum(EngineType, name="engine_type"), default=EngineType.HTTP
    )
    # Parsing config: selectors, pagination rules, status mapping
    parser_config: Mapped[dict] = mapped_column(JSON, default=dict)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    offers: Mapped[list[Offer]] = relationship(
        back_populates="competitor", cascade="all, delete-orphan"
    )
    parse_runs: Mapped[list[ParseRun]] = relationship(
        back_populates="competitor",
        order_by="ParseRun.started_at.desc()",
        cascade="all, delete-orphan",
    )

    @property
    def last_run(self) -> ParseRun | None:
        return self.parse_runs[0] if self.parse_runs else None

    @property
    def status_label(self) -> str:
        """Working / Error (spec §24)."""
        run = self.last_run
        if run is None:
            return "Never run"
        if run.status == RunStatus.RUNNING:
            return "Working"
        if run.status == RunStatus.OK:
            return "Working"
        return "Error"


class Product(Base):
    """Canonical product — combines positions from different competitors (spec §17).

    Identified by OEM / SKU / cross-numbers,
    the name is only an additional factor.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str | None] = mapped_column(String(200))
    article: Mapped[str | None] = mapped_column(String(100))
    oem: Mapped[str | None] = mapped_column(String(100))
    # Analogs / cross-numbers
    cross_numbers: Mapped[list] = mapped_column(JSON, default=list)
    applicability: Mapped[str | None] = mapped_column(String(500))  # SHACMAN, SITRAK...
    category: Mapped[str | None] = mapped_column(String(300))
    subcategory: Mapped[str | None] = mapped_column(String(300))
    first_seen_at: Mapped[date] = mapped_column(Date, server_default=func.current_date())
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    offers: Mapped[list[Offer]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list[Snapshot]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    metrics: Mapped[ProductMetrics | None] = relationship(
        back_populates="product", uselist=False, cascade="all, delete-orphan"
    )

    # Indexes for search and matching (spec §17, 18)
    __table_args__ = (
        Index("ix_products_article", "article"),
        Index("ix_products_oem", "oem"),
        Index("ix_products_brand", "brand"),
    )


class Offer(Base):
    """A specific competitor's offer for a product."""

    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id", ondelete="CASCADE")
    )
    url: Mapped[str] = mapped_column(String(1000))
    # Current state (updated on every parse)
    price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    old_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    discount: Mapped[float | None] = mapped_column(Numeric(12, 2))  # rub.
    discount_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))  # %
    stock_qty: Mapped[int | None] = mapped_column(Integer)  # None = unknown
    stock_status: Mapped[StockStatus] = mapped_column(
        Enum(StockStatus, name="stock_status"), default=StockStatus.UNKNOWN
    )
    warehouse: Mapped[str | None] = mapped_column(String(300))
    delivery_time: Mapped[str | None] = mapped_column(String(200))
    # "Product disappeared from the catalog" (spec §16)
    disappeared: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[date] = mapped_column(Date, server_default=func.current_date())
    last_seen_at: Mapped[date] = mapped_column(Date, server_default=func.current_date())
    last_changed_at: Mapped[date | None] = mapped_column(Date)

    product: Mapped[Product] = relationship(back_populates="offers")
    competitor: Mapped[Competitor] = relationship(back_populates="offers")
    snapshots: Mapped[list[Snapshot]] = relationship(
        back_populates="offer", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("competitor_id", "url", name="uq_offer_competitor_url"),
        Index("ix_offers_product_competitor", "product_id", "competitor_id"),
    )


class Snapshot(Base):
    """Daily snapshot of a product's state (spec §6 — the key requirement).

    Append-only: a row is created once a day and never modified.
    Previous data must not be deleted after a new parse (spec §25).
    """

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id", ondelete="CASCADE")
    )
    day: Mapped[date] = mapped_column(Date)
    price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    stock_qty: Mapped[int | None] = mapped_column(Integer)
    stock_status: Mapped[StockStatus] = mapped_column(
        Enum(StockStatus, name="stock_status")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    offer: Mapped[Offer] = relationship(back_populates="snapshots")
    product: Mapped[Product] = relationship(back_populates="snapshots")

    __table_args__ = (
        UniqueConstraint("offer_id", "day", name="uq_snapshot_offer_day"),
        Index("ix_snapshots_product_day", "product_id", "day"),
        Index("ix_snapshots_competitor_day", "competitor_id", "day"),
    )


class ParseRun(Base):
    """Parse run log for monitoring (spec §24)."""

    __tablename__ = "parse_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id", ondelete="CASCADE")
    )
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.RUNNING
    )
    trigger: Mapped[RunTrigger] = mapped_column(
        Enum(RunTrigger, name="run_trigger"), default=RunTrigger.AUTO
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    pages_processed: Mapped[int] = mapped_column(Integer, default=0)
    products_found: Mapped[int] = mapped_column(Integer, default=0)
    products_new: Mapped[int] = mapped_column(Integer, default=0)
    errors_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)

    competitor: Mapped[Competitor] = relationship(back_populates="parse_runs")


class ChangeEvent(Base):
    """Recorded change between days (spec §7, 15, 20)."""

    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"))
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id", ondelete="CASCADE")
    )
    day: Mapped[date] = mapped_column(Date)
    change_type: Mapped[ChangeType] = mapped_column(Enum(ChangeType, name="change_type"))
    # Δ stock: negative = sale, positive = restock
    delta_qty: Mapped[int | None] = mapped_column(Integer)
    old_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    new_price: Mapped[float | None] = mapped_column(Numeric(12, 2))

    __table_args__ = (
        Index("ix_change_events_day", "day"),
        Index("ix_change_events_product_day", "product_id", "day"),
    )


class ProductMetrics(Base):
    """Recalculated demand metrics (spec §8, 9, 12, 13)."""

    __tablename__ = "product_metrics"

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    # Estimated sales (stock decreases) over periods
    sales_1d: Mapped[int] = mapped_column(Integer, default=0)
    sales_7d: Mapped[int] = mapped_column(Integer, default=0)
    sales_14d: Mapped[int] = mapped_column(Integer, default=0)
    sales_30d: Mapped[int] = mapped_column(Integer, default=0)
    sales_60d: Mapped[int] = mapped_column(Integer, default=0)
    sales_90d: Mapped[int] = mapped_column(Integer, default=0)
    # Average daily demand = sales / days observed
    avg_daily_demand: Mapped[float] = mapped_column(Numeric(10, 3), default=0)
    # Sales frequency: % of days with a stock decrease
    sales_frequency_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    days_observed: Mapped[int] = mapped_column(Integer, default=0)
    # Rating: A / B / C / D / NEW (spec §9)
    rating: Mapped[str] = mapped_column(String(4), default="NEW")
    # Overall demand index 0..100 (spec §12)
    demand_index: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    # How many competitors sell this SKU (spec §11)
    competitors_count: Mapped[int] = mapped_column(Integer, default=0)
    # Price statistics (spec §13)
    price_min: Mapped[float | None] = mapped_column(Numeric(12, 2))
    price_max: Mapped[float | None] = mapped_column(Numeric(12, 2))
    price_avg: Mapped[float | None] = mapped_column(Numeric(12, 2))
    price_median: Mapped[float | None] = mapped_column(Numeric(12, 2))
    replenishments_7d: Mapped[int] = mapped_column(Integer, default=0)
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )

    product: Mapped[Product] = relationship(back_populates="metrics")


class AppSetting(Base):
    """System settings: configurable index weights etc. (spec §12)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
