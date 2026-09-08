"""Bazoš-specific scraper and HTML parser.

Site facts (verified 2026-09-08):
  * personal cars category lives at https://auto.bazos.sk/
  * 20 listings per page, pagination via path suffix /20/, /40/, ...
  * listing card: <div class="inzeraty inzeratyflex">
  * ad URL: /inzerat/{numeric_id}/{slug}.php
  * year/mileage are free-text inside the description preview.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from bazcar.core.exceptions import ParseError, ScraperError
from bazcar.core.models import Listing
from bazcar.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class BazosScraper(BaseScraper):
    def __init__(self, config) -> None:
        super().__init__(config)
        self.page_size = config.base.page_size
        self.base_url = config.base.base_url
        self.search_filters = config.base.search_filters
        self._filters_hash = self._compute_filters_hash()

    @property
    def filters_hash(self) -> str:
        """Short hash identifying the active search filter combination."""
        return self._filters_hash

    def _compute_filters_hash(self) -> str:
        """Compute a short hash from the active search filters."""
        parts = []
        if self.search_filters.query:
            parts.append(f"q={self.search_filters.query}")
        if self.search_filters.min_price is not None:
            parts.append(f"min={self.search_filters.min_price}")
        if self.search_filters.max_price is not None:
            parts.append(f"max={self.search_filters.max_price}")
        if self.search_filters.psc:
            parts.append(f"psc={self.search_filters.psc}")
        if self.search_filters.max_km is not None:
            parts.append(f"km={self.search_filters.max_km}")
        if not parts:
            return "default"
        raw = "|".join(parts)
        return hashlib.md5(raw.encode()).hexdigest()[:8]

    def _query_params(self) -> dict[str, str]:
        """Return the search filter query parameters."""
        params = {}
        if self.search_filters.query:
            params["hledat"] = self.search_filters.query
        if self.search_filters.min_price is not None:
            params["cenaod"] = str(self.search_filters.min_price)
        if self.search_filters.max_price is not None:
            params["cenado"] = str(self.search_filters.max_price)
        if self.search_filters.psc:
            params["psc"] = self.search_filters.psc
        if self.search_filters.max_km is not None:
            params["km_do"] = str(self.search_filters.max_km)
        return params

    def _page_url(self, page_index: int) -> str:
        """Return the URL for a specific page index (0-based)."""
        base = self.base_url
        # Add pagination offset to path
        if page_index > 0:
            offset = page_index * self.page_size
            if not base.endswith("/"):
                base += "/"
            base += f"{offset}/"
        # Append query parameters
        params = self._query_params()
        if params:
            base += "?" + urlencode(params)
        return base

    async def scrape_category(self, max_pages: int = 1) -> list[Listing]:
        """Scrape ``max_pages`` paginated pages using configured search filters."""
        listings: list[Listing] = []
        for page_index in range(max_pages):
            url = self._page_url(page_index)
            html = await self.fetch_text(url)
            page_listings = self.parse_page(html)
            listings.extend(page_listings)
            logger.info("page %d: %d listings (running total %d)", page_index + 1, len(page_listings), len(listings))
        return listings

    async def scrape_detail(self, listing: Listing) -> Listing:
        """Fetch the listing detail page and enrich it with the full description."""
        try:
            html = await self.fetch_text(listing.url)
        except ScraperError as exc:
            logger.warning("detail fetch failed for ad %s: %s", listing.ad_id, exc)
            return listing
        full = self._parse_detail(html)
        if full:
            listing.description = full
        return listing

    def _parse_detail(self, html: str) -> str | None:
        soup = BeautifulSoup(html, "lxml")
        el = soup.select_one(self.config.selectors.detail_description)
        return el.get_text(" ", strip=True) if el else None

    # ---------------------------------------------------------------- parsing

    def parse_page(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "lxml")
        containers = soup.select(self.config.selectors.container)
        if not containers:
            raise ParseError("no listing container found; page structure may have changed")
        listings: list[Listing] = []
        for card in containers:
            listing = self._parse_card(card)
            if listing is not None:
                listings.append(listing)
        return listings

    def _parse_card(self, card) -> Listing | None:
        title_el = card.select_one(self.config.selectors.title)
        if title_el is None:
            return None
        href = title_el.get("href", "")
        title = title_el.get_text(strip=True)
        ad_id = self._extract_id(href)
        if ad_id is None:
            return None
        url = urljoin(self.base_url, href)

        description = card.select_one(self.config.selectors.description)
        description_text = description.get_text(" ", strip=True) if description else ""

        year, mileage = self._extract_year_mileage(title, description_text)

        price_raw = self._read_text(card, self.config.selectors.price)
        price = self._parse_price(price_raw)

        city, postal_code = self._parse_location(card)
        views = self._parse_views(card)
        published_date = self._parse_date(card)

        images = [img.get("src") for img in card.select(self.config.selectors.image) if img.get("src")]

        return Listing(
            ad_id=ad_id,
            url=url,
            title=title,
            price_eur=price,
            price_raw=price_raw,
            city=city,
            postal_code=postal_code,
            published_date=published_date,
            views=views,
            description_preview=description_text,
            image_urls=images,
            year=year,
            mileage_km=mileage,
            source="bazos",
        )

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _read_text(card, selector: str) -> str | None:
        el = card.select_one(selector)
        return el.get_text(" ", strip=True) if el else None

    def _extract_id(self, href: str) -> int | None:
        match = re.search(self.config.markers.ad_path_regex, href)
        return int(match.group(1)) if match else None

    def _parse_price(self, raw: str | None) -> Decimal | None:
        if not raw:
            return None
        text = raw.replace("\u00a0", " ").strip()
        digits = re.sub(r"[^\d]", "", text)
        if not digits:  # "Dohodou", "Vymenné", ...
            return None
        try:
            return Decimal(digits)
        except InvalidOperation:
            return None

    def _parse_location(self, card) -> tuple[str | None, str | None]:
        el = card.select_one(self.config.selectors.location)
        if el is None:
            return None, None
        city = None
        postal = None
        for line in el.stripped_strings:
            text = line.strip()
            if not text:
                continue
            if re.fullmatch(r"\d{3}\s*\d{2}", text):
                postal = text.replace(" ", "")
            elif city is None:
                city = text
        return city, postal

    def _parse_views(self, card) -> int | None:
        raw = self._read_text(card, self.config.selectors.views)
        if not raw:
            return None
        match = re.search(r"(\d+)", raw.replace("\u00a0", ""))
        return int(match.group(1)) if match else None

    def _parse_date(self, card) -> date | None:
        text = self._read_text(card, self.config.selectors.date) or ""
        match = re.search(self.config.markers.date_regex, text)
        if not match:
            return None
        day, month, year = (int(g) for g in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            return None

    def _extract_year_mileage(self, title: str, description: str) -> tuple[int | None, int | None]:
        text = f"{title} | {description}"
        return self._extract_year(text), self._extract_mileage(text)

    def _extract_year(self, text: str) -> int | None:
        for pattern in self.config.markers.year_regexes:
            match = re.search(pattern, text, re.IGNORECASE)
            if match and int(match.group(1)) <= date.today().year + 1:
                return int(match.group(1))
        return None

    def _extract_mileage(self, text: str) -> int | None:
        for pattern in self.config.markers.mileage_regexes:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                value = int(re.sub(r"\D", "", match.group(1)))
                if "tis" in match.group(0).lower():
                    value *= 1000
                if 100 <= value <= 5_000_000:
                    return value
        return None
