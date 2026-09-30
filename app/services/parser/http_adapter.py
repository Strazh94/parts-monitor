"""HTTP-адаптер: конфигурируемый парсинг по CSS-селекторам.

Конфиг хранится в Competitor.parser_config:
{
  "start_urls": ["https://site/catalog/page-{page}"],
  "max_pages": 50,
  "pagination": "page" | "next_button",
  "next_selector": "a.next",
  "item_selector": ".product-card",
  "fields": {
    "name": {"selector": ".title", "attr": "text"},
    "article": {"selector": ".sku"},
    "price": {"selector": ".price", "type": "price"},
    "stock": {"selector": ".availability", "type": "stock"},
    "url": {"selector": "a", "attr": "href"}
  }
}
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.services.parser.normalize import parse_price, parse_stock
from app.services.matching import ParsedItem

logger = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class ParseError(Exception):
    """Ошибка парсинга конкретного сайта."""


class HttpAdapter:
    """Скачивает страницы через httpx и извлекает товары по селекторам."""

    def __init__(self, base_url: str, config: dict, timeout: float = 30.0):
        self.base_url = base_url
        self.config = config
        self.timeout = timeout
        self.pages_processed = 0

    async def fetch_items(self, on_progress=None) -> list[ParsedItem]:
        """Обходит страницы каталога и собирает товары."""
        fields = self.config.get("fields") or {}
        if not fields.get("name") and not fields.get("url"):
            raise ParseError(
                "Не настроены селекторы парсинга (fields) для этого сайта"
            )

        item_selector = self.config.get("item_selector")
        if not item_selector:
            raise ParseError("Не задан item_selector — селектор карточки товара")

        start_urls = self.config.get("start_urls") or [self.base_url]
        max_pages = int(self.config.get("max_pages", 20))
        delay = float(self.config.get("delay_seconds", 1.0))

        items: list[ParsedItem] = []
        headers = {
            "User-Agent": self.config.get("user_agent", DEFAULT_UA),
            "Accept-Language": "ru-RU,ru;q=0.9",
        }

        async with httpx.AsyncClient(
            headers=headers, timeout=self.timeout, follow_redirects=True
        ) as client:
            urls_to_visit = list(start_urls)
            visited: set[str] = set()

            while urls_to_visit and self.pages_processed < max_pages:
                page_url = urls_to_visit.pop(0)
                if page_url in visited:
                    continue
                visited.add(page_url)

                try:
                    html = await self._fetch(client, page_url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Ошибка загрузки %s: %s", page_url, exc)
                    self._stat("errors", 1)
                    continue

                self.pages_processed += 1
                if on_progress:
                    await on_progress(self.pages_processed, len(items))

                soup = BeautifulSoup(html, "lxml")
                page_items = self._extract(soup, page_url, fields, item_selector)
                items.extend(page_items)

                # Пагинация: кнопка "следующая" или шаблон URL
                if self.config.get("pagination") == "next_button":
                    next_sel = self.config.get("next_selector", "a.next")
                    link = soup.select_one(next_sel)
                    if link and link.get("href"):
                        nxt = urljoin(page_url, link["href"])
                        if nxt not in visited:
                            urls_to_visit.append(nxt)
                else:
                    # Шаблон {page} в start_urls
                    for tpl in start_urls:
                        if "{page}" in tpl:
                            current = self._page_number(tpl, page_url)
                            if current and current < max_pages:
                                nxt = tpl.format(page=current + 1)
                                if nxt not in visited:
                                    urls_to_visit.append(nxt)

                # Вежливая задержка между запросами
                if urls_to_visit and delay > 0:
                    await asyncio.sleep(delay)

        if not items:
            raise ParseError(
                "Найдено 0 товаров — вероятно, структура сайта изменилась"
            )
        return items

    async def _fetch(self, client: httpx.AsyncClient, url: str) -> str:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.text
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                await asyncio.sleep(2.0 * (attempt + 1))
        raise ParseError(f"Не удалось загрузить {url}: {last_exc}")

    def _extract(
        self,
        soup: BeautifulSoup,
        page_url: str,
        fields: dict,
        item_selector: str,
    ) -> list[ParsedItem]:
        items = []
        for node in soup.select(item_selector):
            values = {}
            for name, spec in fields.items():
                values[name] = self._field(node, spec, page_url)
            if not values.get("name") and not values.get("url"):
                continue

            price = self._typed_price(values.get("price"))
            old_price = self._typed_price(values.get("old_price"))
            stock_raw = values.get("stock")

            items.append(
                ParsedItem(
                    url=values.get("url") or page_url,
                    name=values.get("name"),
                    article=values.get("article"),
                    oem=values.get("oem"),
                    brand=values.get("brand"),
                    category=values.get("category"),
                    subcategory=values.get("subcategory"),
                    applicability=values.get("applicability"),
                    cross_numbers_raw=values.get("cross_numbers"),
                    price=price,
                    old_price=old_price,
                    stock_raw=stock_raw,
                    warehouse=values.get("warehouse"),
                    delivery_time=values.get("delivery_time"),
                )
            )
        return items

    @staticmethod
    def _field(node, spec: dict, page_url: str) -> str | None:
        if isinstance(spec, str):
            spec = {"selector": spec}
        selector = spec.get("selector")
        if not selector:
            return None
        found = node.select_one(selector) if selector != ":self" else node
        if found is None:
            return None
        attr = spec.get("attr", "text")
        if attr == "text":
            value = found.get_text(" ", strip=True)
        else:
            value = found.get(attr)
            if value and spec.get("absolute_url", True) and attr == "href":
                value = urljoin(page_url, value)
        if value is None:
            return None
        return str(value).strip() or None

    @staticmethod
    def _typed_price(raw: str | None) -> float | None:
        if not raw:
            return None
        return parse_price(raw)

    @staticmethod
    def _page_number(template: str, url: str) -> int | None:
        if "{page}" not in template:
            return None
        prefix, suffix = template.split("{page}")
        if not url.startswith(prefix) or not url.endswith(suffix):
            return None
        mid = url[len(prefix): len(url) - len(suffix) if suffix else len(url)]
        return int(mid) if mid.isdigit() else None

    @staticmethod
    def _stat(key: str, value: int) -> None:
        """Заглушка для счётчиков; используется ParseRun."""
        return None
