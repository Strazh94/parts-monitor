"""Matching products across competitors (spec §17).

Critical function: the same product from different competitors
is merged by OEM / SKU / cross-number.
The name is only an additional factor (used when creating new positions).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product
from app.services.parser.normalize import normalize_key, split_cross_numbers


@dataclass
class ParsedItem:
    """An item parsed from a competitor's page (before matching)."""

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
    """Index of normalized keys -> product.

    Built once per parse run: loads all keys from the DB
    and answers "does this product exist?" in memory.
    """

    def __init__(self, db: Session):
        self.db = db
        # normalized key -> product_id
        self.key_index: dict[str, int] = {}
        self._load()

    def _load(self) -> None:
        products = self.db.execute(
            select(Product.id, Product.oem, Product.article, Product.cross_numbers)
        ).all()
        for pid, oem, article, crosses in products:
            for key in self._keys(oem, article, crosses):
                # The first match wins on collision
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
        """Returns an existing product or creates a new one."""
        crosses = split_cross_numbers(item.cross_numbers_raw)
        for key in self._keys(item.oem, item.article, crosses):
            pid = self.key_index.get(key)
            if pid is not None:
                product = self.db.get(Product, pid)
                if product is not None:
                    self._merge_info(product, item, crosses)
                    return product

        # Not found — create a new canonical product
        product = Product(
            name=(item.name or "Untitled").strip()[:500],
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

        # Register all keys of the new product
        for key in self._keys(product.oem, product.article, crosses):
            self.key_index.setdefault(key, product.id)
        return product

    def _merge_info(self, product: Product, item: ParsedItem, crosses: list[str]) -> None:
        """Fills in empty product fields with data from the new competitor."""
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
