"""Normalization of raw parsing data: prices, availability statuses, keys."""
from __future__ import annotations

import re

from app.models import StockStatus

# --- Prices: "12 500 ₽", "12.500,00 руб.", "1 250" -> 12500.0 ---

_PRICE_RE = re.compile(r"(\d[\d\s\s.,]*)")


def parse_price(raw: str | None) -> float | None:
    """Extracts a number from a price string in any formatting."""
    if not raw:
        return None
    # Remove everything except digits and separators
    cleaned = re.sub(r"[^\d.,\s]", "", raw).strip()
    m = _PRICE_RE.search(cleaned)
    if not m:
        return None
    num = m.group(1).replace(" ", "").replace("\xa0", "")
    if "," in num and "." in num:
        # The last separator is the decimal one
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        tail = num.split(",")[-1]
        num = num.replace(",", ".") if len(tail) == 2 else num.replace(",", "")
    elif num.count(".") > 1:
        num = num.replace(".", "")
    elif "." in num and len(num.split(".")[-1]) not in (2, 3):
        # 12.500 — European thousands format
        num = num.replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


# --- Availability (spec §16): "Много", "В наличии", "Под заказ", "Нет в наличии" ---

_QTY_RE = re.compile(r"(\d+)")

# Order matters: more specific rules come first
_STOCK_RULES: list[tuple[tuple[str, ...], StockStatus]] = [
    (("нет в наличии", "отсутствует", "нет на складе", "н/в"), StockStatus.OUT_OF_STOCK),
    (("под заказ", "по запросу", "ожидается", "предзаказ"), StockStatus.ON_ORDER),
    (("много", "больше 10", ">10", "есть на складе"), StockStatus.MANY),
    (("в наличии", "наличие", "available", "in stock", "есть"), StockStatus.IN_STOCK),
]


def parse_stock(raw: str | None) -> tuple[StockStatus, int | None]:
    """Availability status -> (canonical status, quantity | None).

    - "Нет в наличии" -> 0
    - "Под заказ" -> stock unknown
    - "В наличии" without a number -> in stock, no quantity (not counted as a sale)
    - A number -> (IN_STOCK, number)
    """
    if not raw:
        return StockStatus.UNKNOWN, None
    text = raw.strip().lower().replace("\xa0", " ")
    if not text:
        return StockStatus.UNKNOWN, None

    for keywords, status in _STOCK_RULES:
        for kw in keywords:
            if kw in text:
                if status == StockStatus.OUT_OF_STOCK:
                    return status, 0
                if status == StockStatus.IN_STOCK:
                    # "В наличии: 5" -> quantity; otherwise the quantity is unknown
                    m = _QTY_RE.search(text)
                    return status, int(m.group(1)) if m else None
                return status, None

    # Strictly a number
    if re.fullmatch(r"\d+", text):
        return StockStatus.IN_STOCK, int(text)
    return StockStatus.UNKNOWN, None


# --- Matching keys (spec §17) ---

def normalize_key(value: str | None) -> str | None:
    """Normalizes an OEM/SKU/cross-number for comparison.

    "WG 972 546 013" == "WG972546013" == "wg-972-546-013"
    """
    if not value:
        return None
    key = re.sub(r"[^A-Za-zА-Яа-я0-9]", "", value).upper()
    return key or None


def split_cross_numbers(raw: str | None) -> list[str]:
    """A list of cross-numbers separated by comma/semicolon/slash."""
    if not raw:
        return []
    parts = re.split(r"[,;/]| или ", raw)
    return [p.strip() for p in parts if p.strip()]
