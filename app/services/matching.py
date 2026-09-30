"""Сопоставление товаров разных конкурентов (ТЗ п.17).

Критически важная функция: один и тот же товар у разных конкурентов
объединяется по OEM / артикулу / кросс-номеру.
Название — только дополнительный фактор (для создания новых позиций).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product
from app.services.parser.normalize import normalize_key, split_cross_numbers


@dataclass
class ParsedItem:
    """Элемент, распарсенный со страницы конкурента (до сопоставления)."""

    url: str
    name: str | None = None
    article: str | None = None
    oem: str | None = None
    brand: str | None = None
    category: str | None = None
    subcategory: str | None = None
    applicability: str | None = None
    cross_numbers_raw: str | None = None
    price: float | None = None
    old_price: float | None = None
    stock_raw: str | None = None
    warehouse: str | None = None
    delivery_time: str | None = None
    extra: dict = field(default_factory=dict)


class ProductMatcher:
    """Индекс нормализованных ключей -> продукт.

    Строится один раз на запуск парсинга: загружает все ключи из БД
    и в памяти отвечает на вопрос «есть ли такой товар?».
    """

    def __init__(self, db: Session):
        self.db = db
        # нормализованный ключ -> product_id
        self.key_index: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        products = self.db.execute(
            select(Product.id, Product.oem, Product.article, Product.cross_numbers)
        ).all()
        for pid, oem, article, crosses in products:
            for key in self._keys(oem, article, crosses):
                # Первый найденный побеждает при коллизии
                self.key_index.setdefault(key, pid)

    @staticmethod
    def _keys(oem: str | None, article: str | None, crosses: list | None) -> list[str]:
        keys = []
        for value in [oem, article, *(crosses or [])]:
            k = normalize_key(value)
            if k:
                keys.append(k)
        return keys

    def match(self, item: ParsedItem) -> Product:
        """Возвращает существующий товар или создаёт новый."""
        crosses = split_cross_numbers(item.cross_numbers_raw)
        for key in self._keys(item.oem, item.article, crosses):
            pid = self.key_index.get(key)
            if pid is not None:
                product = self.db.get(Product, pid)
                if product is not None:
                    self._merge_info(product, item, crosses)
                    return product

        # Не найден — создаём новую каноническую позицию
        product = Product(
            name=(item.name or "Без названия").strip()[:500],
            brand=_strip(item.brand),
            article=_strip(item.article),
            oem=_strip(item.oem),
            cross_numbers=crosses,
            applicability=_strip(item.applicability),
            category=_strip(item.category),
            subcategory=_strip(item.subcategory),
        )
        self.db.add(product)
        self.db.flush()

        # Регистрируем все ключи нового товара
        for key in self._keys(product.oem, product.article, crosses):
            self.key_index.setdefault(key, product.id)
        return product

    def _merge_info(self, product: Product, item: ParsedItem, crosses: list[str]) -> None:
        """Дозаполняет пустые поля позиции данными из нового конкурента."""
        changed = False
        if not product.brand and item.brand:
            product.brand = _strip(item.brand)
            changed = True
        if not product.applicability and item.applicability:
            product.applicability = _strip(item.applicability)
            changed = True
        if not product.category and item.category:
            product.category = _strip(item.category)
            changed = True
        if crosses:
            existing = set(product.cross_numbers or [])
            new = [c for c in crosses if normalize_key(c) not in {
                normalize_key(x) for x in existing
            }]
            if new:
                product.cross_numbers = (product.cross_numbers or []) + new
                for k in self._keys(None, None, new):
                    self.key_index.setdefault(k, product.id)
                changed = True
        if changed:
            self.db.flush()


def _strip(value: str | None, limit: int = 500) -> str | None:
    if not value:
        return None
    value = value.strip()
    return value[:limit] if value else None
