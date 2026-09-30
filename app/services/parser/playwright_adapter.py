"""Playwright adapter for JavaScript sites (spec §26)."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urljoin

from app.services.matching import ParsedItem
from app.services.parser.normalize import parse_price
from app.services.parser.http_adapter import DEFAULT_UA, ParseError

logger = logging.getLogger(__name__)


class PlaywrightAdapter:
    """Renders pages in a headless browser and extracts products."""

    def __init__(self, base_url: str, config: dict, timeout_ms: int = 30000):
        self.base_url = base_url
        self.config = config
        self.timeout_ms = timeout_ms
        self.pages_processed = 0

    async def fetch_items(self, on_progress=None) -> list[ParsedItem]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ParseError(
                "Playwright is not installed: pip install playwright && playwright install chromium"
            ) from exc

        fields = self.config.get("fields") or {}
        item_selector = self.config.get("item_selector")
        if not item_selector:
            raise ParseError("item_selector is not set for Playwright parsing")

        start_urls = self.config.get("start_urls") or [self.base_url]
        max_pages = int(self.config.get("max_pages", 20))
        delay = float(self.config.get("delay_seconds", 2.0))

        items: list[ParsedItem] = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=self.config.get("user_agent", DEFAULT_UA),
                locale="ru-RU",
            )
            page = await context.new_page()

            urls_to_visit = list(start_urls)
            visited: set[str] = set()

            while urls_to_visit and self.pages_processed < max_pages:
                page_url = urls_to_visit.pop(0)
                if page_url in visited:
                    continue
                visited.add(page_url)

                try:
                    await page.goto(page_url, timeout=self.timeout_ms, wait_until="networkidle")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Playwright: failed to load %s: %s", page_url, exc)
                    continue

                self.pages_processed += 1
                if on_progress:
                    await on_progress(self.pages_processed, len(items))

                # Scroll so that lazy-loaded products appear
                await self._scroll(page)

                items.extend(
                    await self._extract(page, page_url, fields, item_selector)
                )

                if self.config.get("pagination") == "next_button":
                    next_sel = self.config.get("next_selector", "a.next")
                    try:
                        link = page.locator(next_sel).first
                        if await link.count() > 0 and await link.is_visible():
                            href = await link.get_attribute("href")
                            if href:
                                nxt = urljoin(page_url, href)
                                if nxt not in visited:
                                    urls_to_visit.append(nxt)
                    except Exception:  # noqa: BLE001
                        pass

                if urls_to_visit and delay > 0:
                    await asyncio.sleep(delay)

            await context.close()
            await browser.close()

        if not items:
            raise ParseError(
                "Found 0 products — the site structure has probably changed"
            )
        return items

    @staticmethod
    async def _scroll(page) -> None:
        try:
            height = await page.evaluate("document.body.scrollHeight")
            step = max(height // 5, 400)
            for y in range(0, height, step):
                await page.evaluate(f"window.scrollTo(0, {y})")
                await asyncio.sleep(0.3)
            await page.evaluate("window.scrollTo(0, 0)")
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _extract(
        page, page_url: str, fields: dict, item_selector: str
    ) -> list[ParsedItem]:
        """Extraction via JS in the page context — faster than per-element queries."""
        rows = await page.eval_on_selector_all(
            item_selector,
            """(nodes, spec) => nodes.map(node => {
                const out = {};
                for (const [name, s] of Object.entries(spec.fields || {})) {
                    const sel = typeof s === 'string' ? s : s.selector;
                    const attr = (typeof s === 'object' && s.attr) ? s.attr : 'text';
                    if (!sel) continue;
                    const el = sel === ':self' ? node : node.querySelector(sel);
                    if (!el) continue;
                    out[name] = attr === 'text' ? (el.textContent || '').trim() : el.getAttribute(attr);
                }
                return out;
            })""",
            {"fields": fields},
        )

        items = []
        for row in rows:
            url = row.get("url") or page_url
            if row.get("url"):
                url = urljoin(page_url, row["url"])
            name = row.get("name")
            if not name and not row.get("url"):
                continue
            items.append(
                ParsedItem(
                    url=url,
                    name=name,
                    article=row.get("article"),
                    oem=row.get("oem"),
                    brand=row.get("brand"),
                    category=row.get("category"),
                    subcategory=row.get("subcategory"),
                    applicability=row.get("applicability"),
                    cross_numbers_raw=row.get("cross_numbers"),
                    price=parse_price(row.get("price")),
                    old_price=parse_price(row.get("old_price")),
                    stock_raw=row.get("stock"),
                    warehouse=row.get("warehouse"),
                    delivery_time=row.get("delivery_time"),
                )
            )
        return items
