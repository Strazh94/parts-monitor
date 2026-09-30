"""Модели базы данных.

История остатков хранится в append-only таблице snapshots (ТЗ п.6) —
значения предыдущих дней никогда не перезаписываются.
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
    """Канонические статусы наличия (ТЗ п.16)."""

    IN_STOCK = "in_stock"          # в наличии (без количества или с ним)
    OUT_OF_STOCK = "out_of_stock"  # нет в наличии -> 0
    ON_ORDER = "on_order"          # под заказ -> остаток неизвестен
    MANY = "many"                  # "много" -> количество неизвестно
    UNKNOWN = "unknown"            # данные не получены


class EngineType(str, enum.Enum):
    """Тип движка парсинга сайта."""

    HTTP = "http"          # requests/httpx + BeautifulSoup
    PLAYWRIGHT = "playwright"  # для сайтов на JavaScript


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"


class RunTrigger(str, enum.Enum):
    AUTO = "auto"    # по расписанию
    MANUAL = "manual"  # кнопка "Запустить сбор данных"


class ChangeType(str, enum.Enum):
    """Тип изменения между днями (ТЗ п.7, 15)."""

    SALES = "sales"                    # снижение остатка -> предполагаемая продажа
    REPLENISHMENT = "replenishment"    # рост остатка -> поступление
    PRICE_DOWN = "price_down"
    PRICE_UP = "price_up"
    APPEARED = "appeared"              # новый товар
    DISAPPEARED = "disappeared"        # товар исчез из каталога
    STATUS_ONLY = "status_only"        # изменился статус наличия без количества


class Competitor(Base):
    """Сайт конкурента (ТЗ п.4, 23)."""

    __tablename__ = "competitors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    engine: Mapped[EngineType] = mapped_column(
        Enum(EngineType, name="engine_type"), default=EngineType.HTTP
    )
    # Конфиг парсинга: селекторы, правила пагинации, маппинг статусов
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
        """Работает / Ошибка (ТЗ п.24)."""
        run = self.last_run
        if run is None:
            return "Не запускался"
        if run.status == RunStatus.RUNNING:
            return "Работает"
        if run.status == RunStatus.OK:
            return "Работает"
        return "Ошибка"


class Product(Base):
    """Канонический товар — объединяет позиции разных конкурентов (ТЗ п.17).

    Идентификация по OEM / артикулу / кросс-номерам,
    название — только дополнительный фактор.
    """

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    brand: Mapped[str | None] = mapped_column(String(200))
    article: Mapped[str | None] = mapped_column(String(100))
    oem: Mapped[str | None] = mapped_column(String(100))
    # Аналоги / кросс-номера
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

    # Индексы для поиска и сопоставления (ТЗ п.17, 18)
    __table_args__ = (
        Index("ix_products_article", "article"),
        Index("ix_products_oem", "oem"),
        Index("ix_products_brand", "brand"),
    )


class Offer(Base):
    """Предложение конкретного конкурента о товаре."""

    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id", ondelete="CASCADE")
    )
    url: Mapped[str] = mapped_column(String(1000))
    # Текущее состояние (обновляется при каждом парсинге)
    price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    old_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    discount: Mapped[float | None] = mapped_column(Numeric(12, 2))  # руб.
    discount_pct: Mapped[float | None] = mapped_column(Numeric(5, 2))  # %
    stock_qty: Mapped[int | None] = mapped_column(Integer)  # None = неизвестно
    stock_status: Mapped[StockStatus] = mapped_column(
        Enum(StockStatus, name="stock_status"), default=StockStatus.UNKNOWN
    )
    warehouse: Mapped[str | None] = mapped_column(String(300))
    delivery_time: Mapped[str | None] = mapped_column(String(200))
    # "Товар исчез из каталога" (ТЗ п.16)
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
    """Ежедневный снимок состояния товара (ТЗ п.6 — главное требование).

    Append-only: строка создаётся один раз в день и не изменяется.
    Нельзя удалять предыдущие данные после нового парсинга (ТЗ п.25).
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
    """Журнал запуска парсинга для контроля работы (ТЗ п.24)."""

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
    """Зафиксированное изменение между днями (ТЗ п.7, 15, 20)."""

    __tablename__ = "change_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id", ondelete="CASCADE"))
    competitor_id: Mapped[int] = mapped_column(
        ForeignKey("competitors.id", ondelete="CASCADE")
    )
    day: Mapped[date] = mapped_column(Date)
    change_type: Mapped[ChangeType] = mapped_column(Enum(ChangeType, name="change_type"))
    # Δ остатка: отрицательное = продажа, положительное = поступление
    delta_qty: Mapped[int | None] = mapped_column(Integer)
    old_price: Mapped[float | None] = mapped_column(Numeric(12, 2))
    new_price: Mapped[float | None] = mapped_column(Numeric(12, 2))

    __table_args__ = (
        Index("ix_change_events_day", "day"),
        Index("ix_change_events_product_day", "product_id", "day"),
    )


class ProductMetrics(Base):
    """Пересчитываемые метрики спроса (ТЗ п.8, 9, 12, 13)."""

    __tablename__ = "product_metrics"

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    # Предполагаемые продажи (снижение остатков) за периоды
    sales_1d: Mapped[int] = mapped_column(Integer, default=0)
    sales_7d: Mapped[int] = mapped_column(Integer, default=0)
    sales_14d: Mapped[int] = mapped_column(Integer, default=0)
    sales_30d: Mapped[int] = mapped_column(Integer, default=0)
    sales_60d: Mapped[int] = mapped_column(Integer, default=0)
    sales_90d: Mapped[int] = mapped_column(Integer, default=0)
    # Среднедневной спрос = продажи / дни наблюдения
    avg_daily_demand: Mapped[float] = mapped_column(Numeric(10, 3), default=0)
    # Частота продаж: % дней со снижением остатка
    sales_frequency_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    days_observed: Mapped[int] = mapped_column(Integer, default=0)
    # Рейтинг: A / B / C / D / NEW (ТЗ п.9)
    rating: Mapped[str] = mapped_column(String(4), default="NEW")
    # Сводный индекс спроса 0..100 (ТЗ п.12)
    demand_index: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    # Сколько конкурентов продают артикул (ТЗ п.11)
    competitors_count: Mapped[int] = mapped_column(Integer, default=0)
    # Статистика цен (ТЗ п.13)
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
    """Настройки системы: настраиваемые веса индекса и пр. (ТЗ п.12)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSON)
