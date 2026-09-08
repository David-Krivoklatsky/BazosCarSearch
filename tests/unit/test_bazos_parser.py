"""Parser tests against a realistic snapshot of the Bazoš listing HTML."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from bazcar.scrapers.bazos import BazosScraper


def test_parse_page_extracts_all_cards(bazos_scraper: BazosScraper, bazos_page_html: str) -> None:
    listings = bazos_scraper.parse_page(bazos_page_html)
    assert len(listings) == 4


def test_parse_card_full_fields(bazos_scraper: BazosScraper, bazos_page_html: str) -> None:
    listing = bazos_scraper.parse_page(bazos_page_html)[0]
    assert listing.ad_id == 195357798
    assert listing.url == "https://auto.bazos.sk/inzerat/195357798/kia-sportage-1-6-t-gdi-platinum-2023.php"
    assert listing.title == "Kia Sportage 1.6 T-GDI Platinum 2023"
    assert listing.price_eur == Decimal("22500")
    assert listing.price_raw == "22 500 €"
    assert listing.city == "Nitra"
    assert listing.postal_code == "94901"
    assert listing.views == 12
    assert str(listing.published_date) == "2026-09-08"
    assert listing.year == 2022
    assert listing.mileage_km == 33650
    assert listing.image_urls == ["https://www.bazos.sk/img/1t/798/195357798.jpg?t="]


def test_parse_price_dohodou(bazos_scraper: BazosScraper, bazos_page_html: str) -> None:
    listing = bazos_scraper.parse_page(bazos_page_html)[1]
    assert listing.price_eur is None
    assert listing.price_raw == "Dohodou"


def test_parse_year_iv_and_tis_km(bazos_scraper: BazosScraper, bazos_page_html: str) -> None:
    octavia = bazos_scraper.parse_page(bazos_page_html)[1]
    assert octavia.year == 2019
    assert octavia.mileage_km == 251000

    focus = bazos_scraper.parse_page(bazos_page_html)[2]
    assert focus.year == 2016
    assert focus.mileage_km == 128000


def test_parse_swap_and_missing_postal_code(bazos_scraper: BazosScraper, bazos_page_html: str) -> None:
    opel = bazos_scraper.parse_page(bazos_page_html)[3]
    assert opel.price_eur is None
    assert opel.city == "Žilina"
    assert opel.postal_code is None


def test_page_url_building(bazos_scraper: BazosScraper, scraper_config) -> None:
    # Default config carries search filters -> base URL includes query params
    assert bazos_scraper._page_url(0) == "https://auto.bazos.sk/?cenaod=1000&cenado=3000"
    assert bazos_scraper._page_url(1) == "https://auto.bazos.sk/20/?cenaod=1000&cenado=3000"
    assert bazos_scraper._page_url(2) == "https://auto.bazos.sk/40/?cenaod=1000&cenado=3000"

    # With filters - base URL includes query params
    from bazcar.config.settings import SearchFiltersConfig
    filters = SearchFiltersConfig(query="kia", min_price=1000, max_price=20000)
    scraper_config.base.search_filters = filters
    from bazcar.scrapers.bazos import BazosScraper
    filtered_scraper = BazosScraper(scraper_config)
    url0 = filtered_scraper._page_url(0)
    assert url0.startswith("https://auto.bazos.sk/?hledat=kia&cenaod=1000&cenado=20000")
    url1 = filtered_scraper._page_url(1)
    assert url1.startswith("https://auto.bazos.sk/20/?hledat=kia&cenaod=1000&cenado=20000")


def test_scraper_lifecycle_under_asyncio(bazos_scraper: BazosScraper) -> None:
    async def close() -> None:
        await bazos_scraper.close()

    asyncio.run(close())


def test_parse_detail_extracts_full_description(bazos_scraper: BazosScraper) -> None:
    detail_html = (
        open("tests/fixtures/bazos_detail.html", encoding="utf-8").read()
    )
    full = bazos_scraper._parse_detail(detail_html)
    assert full is not None
    assert "Predám SUV Kia Sportage" in full
    assert "Rok výroby: 11/2022" in full
    assert "33 650 km" in full


def test_parse_detail_returns_none_without_container() -> None:
    from bazcar.config.settings import load_scraper_config
    from bazcar.scrapers.bazos import BazosScraper

    cfg = load_scraper_config()
    scraper = BazosScraper(cfg)
    assert scraper._parse_detail("<html><body>no detail here</body></html>") is None


def test_parse_detail_extracts_all_gallery_images(bazos_scraper: BazosScraper) -> None:
    detail_html = (
        open("tests/fixtures/bazos_detail.html", encoding="utf-8").read()
    )
    images = bazos_scraper._parse_detail_images(detail_html)
    assert images == [
        "https://www.bazos.sk/img/1/798/195357798.jpg?t=1",
        "https://www.bazos.sk/img/2/798/195357798.jpg?t=1",
        "https://www.bazos.sk/img/3/798/195357798.jpg?t=1",
    ]


def test_merge_images_dedupes_and_preserves_order() -> None:
    existing = ["https://www.bazos.sk/img/1t/531/195360531.jpg"]
    extra = [
        "https://www.bazos.sk/img/1/531/195360531.jpg?t=1",
        "https://www.bazos.sk/img/2/531/195360531.jpg?t=1",
        "https://www.bazos.sk/img/1t/531/195360531.jpg",  # duplicate cover
    ]
    assert BazosScraper._merge_images(existing, extra) == [
        "https://www.bazos.sk/img/1t/531/195360531.jpg",
        "https://www.bazos.sk/img/1/531/195360531.jpg?t=1",
        "https://www.bazos.sk/img/2/531/195360531.jpg?t=1",
    ]


def test_scrape_detail_merges_gallery_into_card_images(bazos_scraper: BazosScraper) -> None:
    from bazcar.core.models import Listing

    listing = Listing(
        ad_id=195357798,
        url="https://auto.bazos.sk/inzerat/195357798/kia-sportage.php",
        title="Kia Sportage",
        price_eur=None,
        image_urls=["https://www.bazos.sk/img/1t/798/195357798.jpg"],
        description_preview="preview",
    )
    detail_html = open("tests/fixtures/bazos_detail.html", encoding="utf-8").read()
    with patch.object(BazosScraper, "fetch_text", new_callable=AsyncMock, return_value=detail_html):
        enriched = asyncio.run(bazos_scraper.scrape_detail(listing))
    assert enriched.image_urls == [
        "https://www.bazos.sk/img/1t/798/195357798.jpg",
        "https://www.bazos.sk/img/1/798/195357798.jpg?t=1",
        "https://www.bazos.sk/img/2/798/195357798.jpg?t=1",
        "https://www.bazos.sk/img/3/798/195357798.jpg?t=1",
    ]
    assert enriched.description is not None
