"""Live integration test - hits the real Bazoš. Run with: pytest -m live"""

from __future__ import annotations

import pytest

from bazcar.config.settings import load_scraper_config
from bazcar.scrapers.bazos import BazosScraper

pytestmark = pytest.mark.live


@pytest.mark.asyncio
async def test_live_scrape_returns_at_least_50_listings() -> None:
    config = load_scraper_config()
    async with BazosScraper(config) as scraper:
        listings = await scraper.scrape_category(config.base.base_url, max_pages=3)
    assert len(listings) >= 50
    assert len({listing.ad_id for listing in listings}) == len(listings)
    non_null = sum(1 for listing in listings if listing.price_eur is not None)
    assert non_null > len(listings) // 2


@pytest.mark.asyncio
async def test_detail_enrichment_returns_full_description() -> None:
    config = load_scraper_config()
    async with BazosScraper(config) as scraper:
        listings = await scraper.scrape_category(config.base.base_url, max_pages=1)
        sample = listings[:3]
        for listing in sample:
            enriched = await scraper.scrape_detail(listing)
            assert enriched.description is not None
            assert len(enriched.description) > len(enriched.description_preview)
