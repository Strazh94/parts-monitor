"""Нормализация сырых данных парсинга: цены, статусы наличия, ключи."""
from __future__ import annotations

import re

from app.models import StockStatus

# --- Цены: "12 500 ₽", "12.500,00 руб.", "1 250" -> 12500.0 ---

_PRICE_RE = re.compile(r"(\d[\d\s\s.,]*)")


def parse_price(raw: str | None) -> float | None:
    """Извлекает число из строки цены любой форматировки."""
    if not raw:
        return None
    # Убираем всё кроме цифр и разделителей
    cleaned = re.sub(r"[^\d.,\s]", "", raw).strip()
    m = _PRICE_RE.search(cleaned)
    if not m:
        return None
    num = m.group(1).replace(" ", "").replace("\xa0", "")
    if "," in num and "." in num:
        # Последний разделитель — десятичный
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
        # 12.500 — европейский формат тысяч
        num = num.replace(".", "")
    try:
        return float(num)
    except ValueError:
        return None


# --- Наличие (ТЗ п.16): "Много", "В наличии", "Под заказ", "Нет в наличии" ---

_QTY_RE = re.compile(r"(\d+)")

# Порядок важен: более специфичные правила раньше
_STOCK_RULES: list[tuple[tuple[str, ...], StockStatus]] = [
    (("нет в наличии", "отсутствует", "нет на складе", "н/в"), StockStatus.OUT_OF_STOCK),
    (("под заказ", "по запросу", "ожидается", "предзаказ"), StockStatus.ON_ORDER),
    (("много", "больше 10", ">10", "есть на складе"), StockStatus.MANY),
    (("в наличии", "наличие", "available", "in stock", "есть"), StockStatus.IN_STOCK),
]


def parse_stock(raw: str | None) -> tuple[StockStatus, int | None]:
    """Статус наличия -> (канонический статус, количество | None).

    - "Нет в наличии" -> 0
    - "Под заказ" -> остаток неизвестен
    - "В наличии" без числа -> наличие есть, количества нет (продажи не считаем)
    - Число -> (IN_STOCK, число)
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
                    # "В наличии: 5" -> количество; иначе количество неизвестно
                    m = _QTY_RE.search(text)
                    return status, int(m.group(1)) if m else None
                return status, None

    # Строго число
    if re.fullmatch(r"\d+", text):
        return StockStatus.IN_STOCK, int(text)
    return StockStatus.UNKNOWN, None


# --- Ключи сопоставления (ТЗ п.17) ---

def normalize_key(value: str | None) -> str | None:
    """Нормализация OEM/артикула/кросс-номера для сравнения.

    "WG 972 546 013" == "WG972546013" == "wg-972-546-013"
    """
    if not value:
        return None
    key = re.sub(r"[^A-Za-zА-Яа-я0-9]", "", value).upper()
    return key or None


def split_cross_numbers(raw: str | None) -> list[str]:
    """Список кросс-номеров через запятую/точку с запятой/слэш."""
    if not raw:
        return []
    parts = re.split(r"[,;/]| или ", raw)
    return [p.strip() for p in parts if p.strip()]
